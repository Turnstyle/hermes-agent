import { describe, expect, it } from 'vitest'

import { graftRefreshedTailOntoBackfill } from '@/app/chat/transcript-backfill'
import type { ChatMessage } from '@/lib/chat-messages'

import { preserveLocalPendingTurnMessages } from './utils'

const m = (id: string, role: ChatMessage['role'], text: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text }],
  ...extra
})

// Codex checker probes B1-B3 (t_edb5b1dd REVIEW3.json), kept verbatim as regressions.
describe('occurrence receipts: checker probes', () => {
  it('a row naming another occurrence cannot consume an unreceipted equal send by ordinal', () => {
    const prev = [m('user-new', 'user', 'repeat')]
    const next = [m('stored-old', 'user', 'repeat', { rowId: 8, clientMessageIds: ['user-old'] })]
    expect(preserveLocalPendingTurnMessages(next, prev).map(m => m.id)).toContain('user-new')
  })
  it('a receipted optimistic row in a retained prefix is removed when its saved row has aged out of the tail', () => {
    const pending = m('user-A', 'user', 'one', { rowId: 12, clientMessageIds: ['user-A'] })
    const saved = m('stored-A', 'user', 'one', { rowId: 12, clientMessageIds: ['user-A'] })
    const anchor = m('anchor', 'assistant', 'later', { rowId: 200 })
    const prev = [m('old', 'user', 'older', { rowId: 5 }), pending, saved, anchor]
    const tail = [anchor, m('new', 'assistant', 'latest', { rowId: 201 })]
    const out = preserveLocalPendingTurnMessages(graftRefreshedTailOntoBackfill(tail, prev), prev)
    expect(out.map(m => m.id)).not.toContain('user-A')
    expect(out.map(m => m.id)).toContain('stored-A')
  })
  it('a cached saved occurrence still answers its bubble when refresh has no overlap and receipt was missed', () => {
    const prev = [
      m('saved', 'user', 'one', { rowId: 12, clientMessageIds: ['user-A'] }),
      m('reply', 'assistant', 'answer', { rowId: 13 }),
      m('user-A', 'user', 'one')
    ]

    const tail = [m('new', 'user', 'later', { rowId: 200 }), m('answer', 'assistant', 'latest', { rowId: 201 })]
    const out = preserveLocalPendingTurnMessages(graftRefreshedTailOntoBackfill(tail, prev), prev)
    expect(out.map(m => m.id)).not.toContain('user-A')
  })
})
