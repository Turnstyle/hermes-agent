import type { PromptReceiptPayload } from '@hermes/shared'

import type { ChatMessage } from '@/lib/chat-messages'

import type { ClientSessionState } from '../../../../types'

import type { GatewayEventContext } from './types'

function receiptPayload(raw: unknown): PromptReceiptPayload | null {
  if (!raw || typeof raw !== 'object') {
    return null
  }

  const { client_message_ids: ids, status, user_row_id: rowId } = raw as Partial<PromptReceiptPayload>

  if (!Array.isArray(ids) || (status !== 'persisted' && status !== 'dropped')) {
    return null
  }

  const clientIds = ids.filter((id): id is string => typeof id === 'string' && id.length > 0)

  if (!clientIds.length) {
    return null
  }

  if (status === 'persisted' && !(typeof rowId === 'number' && Number.isSafeInteger(rowId) && rowId > 0)) {
    return null // persistence unproven: never retire a bubble on it
  }

  return { client_message_ids: clientIds, status, ...(status === 'persisted' ? { user_row_id: rowId } : {}) }
}

/** Apply a receipt to one session's messages; returns the same array when nothing matched. */
export function applyPromptReceipt(messages: ChatMessage[], receipt: PromptReceiptPayload): ChatMessage[] {
  const ids = new Set(receipt.client_message_ids)
  const isTarget = (message: ChatMessage) => message.role === 'user' && ids.has(message.id)

  if (!messages.some(isTarget)) {
    return messages
  }

  if (receipt.status === 'dropped') {
    // The gateway discarded this queued copy as a duplicate of the live turn's own prompt,
    // which is already on screen: the copy will never get a row, so it must not wait forever.
    return messages.filter(message => !isTarget(message))
  }

  const rowId = receipt.user_row_id as number

  return messages.map(message =>
    isTarget(message) && (message.rowId !== rowId || !message.clientMessageIds?.length)
      ? { ...message, rowId, clientMessageIds: receipt.client_message_ids }
      : message
  )
}

/**
 * `prompt.receipt`: the durable user row for one or more sends now exists (or a queued copy was
 * dropped). Binds each optimistic bubble to its row so reconcile keeps or retires it by identity.
 * A send that sat in the busy queue gets no `user_row_id` on its RPC reply; this is its only proof.
 */
export function handlePromptReceiptEvent(ctx: GatewayEventContext): boolean {
  const { deps, event, payload, sessionId } = ctx

  if (event.type !== 'prompt.receipt') {
    return false
  }

  const receipt = receiptPayload(payload)

  if (!receipt) {
    return true
  }

  // The bubble normally lives in the routed runtime; a submit recovered onto a fresh runtime
  // mid-flight can leave it in another slice, so fall back to whichever state holds the id.
  const states = deps.sessionStateByRuntimeIdRef.current

  const holds = (state: ClientSessionState | undefined) =>
    Boolean(state?.messages.some(m => m.role === 'user' && receipt.client_message_ids.includes(m.id)))

  const target =
    sessionId && holds(states.get(sessionId)) ? sessionId : [...states.entries()].find(([, state]) => holds(state))?.[0]

  if (target) {
    deps.updateSessionState(target, state => {
      const messages = applyPromptReceipt(state.messages, receipt)

      return messages === state.messages ? state : { ...state, messages }
    })
  }

  return true
}
