import { describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import type { ClientSessionState } from '../../../../types'

import { applyPromptReceipt, handlePromptReceiptEvent } from './prompt-receipt'
import type { GatewayEventContext } from './types'

const user = (id: string, text: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ id, role: 'user', parts: [{ type: 'text', text }], ...extra }) as ChatMessage

describe('applyPromptReceipt', () => {
  it('binds every named bubble to the shared durable row', () => {
    const messages = [user('user-A', 'one'), user('user-B', 'two'), user('other', 'x')]

    const out = applyPromptReceipt(messages, {
      client_message_ids: ['user-A', 'user-B'],
      status: 'persisted',
      user_row_id: 42
    })

    expect(out.map(m => [m.id, m.rowId, m.clientMessageIds])).toEqual([
      ['user-A', 42, ['user-A', 'user-B']],
      ['user-B', 42, ['user-A', 'user-B']],
      ['other', undefined, undefined]
    ])
  })

  it('removes a queued copy the gateway dropped as a live-turn duplicate', () => {
    const messages = [user('user-live', 'run tests', { rowId: 7 }), user('user-dup', 'run tests')]

    expect(
      applyPromptReceipt(messages, { client_message_ids: ['user-dup'], status: 'dropped' }).map(m => m.id)
    ).toEqual(['user-live'])
  })

  it('returns the same array when no bubble matches', () => {
    const messages = [user('user-A', 'one')]

    expect(applyPromptReceipt(messages, { client_message_ids: ['nope'], status: 'dropped' })).toBe(messages)
  })
})

describe('handlePromptReceiptEvent', () => {
  function run(payload: unknown, states: Map<string, ClientSessionState>, sessionId: null | string = 'rt-1') {
    const updateSessionState = vi.fn((sid: string, updater: (state: ClientSessionState) => ClientSessionState) => {
      const next = updater(states.get(sid) as ClientSessionState)
      states.set(sid, next)

      return next
    })

    const ctx = {
      deps: { sessionStateByRuntimeIdRef: { current: states }, updateSessionState },
      event: { type: 'prompt.receipt', session_id: sessionId ?? '', payload },
      payload,
      sessionId
    } as unknown as GatewayEventContext

    return { consumed: handlePromptReceiptEvent(ctx), updateSessionState }
  }

  it('ignores other events', () => {
    const ctx = { event: { type: 'message.complete' } } as unknown as GatewayEventContext

    expect(handlePromptReceiptEvent(ctx)).toBe(false)
  })

  it('never binds on an unproven row id', () => {
    const states = new Map([['rt-1', { messages: [user('user-A', 'one')] } as ClientSessionState]])
    const { consumed, updateSessionState } = run({ client_message_ids: ['user-A'], status: 'persisted' }, states)

    expect(consumed).toBe(true)
    expect(updateSessionState).not.toHaveBeenCalled()
  })

  it('finds the bubble in another runtime slice after a mid-send recovery', () => {
    const states = new Map([
      ['rt-1', { messages: [] as ChatMessage[] } as ClientSessionState],
      ['rt-2', { messages: [user('user-A', 'one')] } as ClientSessionState]
    ])

    run({ client_message_ids: ['user-A'], status: 'persisted', user_row_id: 9 }, states)

    expect(states.get('rt-2')?.messages[0].rowId).toBe(9)
  })
})
