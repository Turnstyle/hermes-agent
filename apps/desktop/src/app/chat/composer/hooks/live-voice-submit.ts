import { freshDraftScope, stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { enqueueQueuedPrompt, serverOwnsComposerQueue } from '@/store/composer-queue'

import type { ChatBarProps } from '../types'

interface BusyLiveVoiceInput {
  mode: 'queue' | 'steer'
  text: string
  voiceContext: string
  sessionId: string | null | undefined
  queueSessionKey: ChatBarProps['queueSessionKey']
  onSteer: ChatBarProps['onSteer']
  onSubmit: ChatBarProps['onSubmit']
}

function keepTranscript(owner: string, text: string) {
  const draft = takeSessionDraft(owner)
  const combined = draft.text ? `${draft.text}\n${text}` : text
  stashSessionDraft(owner, combined, draft.attachments)
}

export async function submitBusyLiveVoiceTurn({
  mode,
  text,
  voiceContext,
  sessionId,
  queueSessionKey,
  onSteer,
  onSubmit
}: BusyLiveVoiceInput): Promise<void> {
  // Resolve the same admission ladder as typed steering before the await:
  // a pane switch must not re-home a refused transcript or its voice context.
  const ownerSession = sessionId ?? null
  const ownerQueue = queueSessionKey ?? null
  const ownerDraft = ownerQueue ?? freshDraftScope()
  const canonical = serverOwnsComposerQueue(ownerSession ?? ownerQueue)
  const submit = onSubmit
  const options = { surface: 'voice-live', voiceContext } as const

  try {
    if (mode === 'steer' && (await onSteer?.(text, 'steer'))) {
      return
    }
  } catch {
    // A failed steer still owns the user's words; keep them via admission.
  }

  if (!ownerQueue) {
    keepTranscript(ownerDraft, text)
  } else if (canonical) {
    try {
      const accepted = await submit(text, {
        ...options,
        fromQueue: true,
        attachments: [],
        sessionId: ownerSession,
        storedSessionId: ownerQueue
      })

      if (accepted !== true) {
        throw new Error('Voice follow-up was not admitted')
      }
    } catch (error) {
      keepTranscript(ownerDraft, text)
      throw error
    }
  } else if (!enqueueQueuedPrompt(ownerQueue, { text, attachments: [], ...options })) {
    keepTranscript(ownerDraft, text)
  }
}
