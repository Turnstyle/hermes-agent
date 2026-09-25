import { describe, expect, it } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'

describe('hydration: display_metadata.client_message_ids (t_edb5b1dd)', () => {
  it('maps a user row receipt onto clientMessageIds, from object or legacy JSON text', () => {
    const [a, b, c] = toChatMessages([
      { id: 1, role: 'user', content: 'one', timestamp: 1, display_metadata: { client_message_ids: ['user-A', 7] } },
      { id: 2, role: 'user', content: 'two', timestamp: 2, display_metadata: '{"client_message_ids":["user-B"]}' },
      { id: 3, role: 'user', content: 'three', timestamp: 3 }
    ] as Parameters<typeof toChatMessages>[0])

    expect(a.clientMessageIds).toEqual(['user-A'])
    expect(b.clientMessageIds).toEqual(['user-B'])
    expect(c.clientMessageIds).toBeUndefined()
  })
})
