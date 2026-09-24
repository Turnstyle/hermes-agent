import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getAllSessionMessages } from '@/hermes'
import { notifyError } from '@/store/notifications'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { exportSession } from './session-export'

vi.mock('@/hermes', () => ({
  getAllSessionMessages: vi.fn()
}))

vi.mock('@/store/notifications', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return { ...actual, notify: vi.fn(), notifyError: vi.fn() }
})

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: '20260924_remote1',
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile: 'default',
    source: 'cli',
    started_at: 1_000,
    title: 'Remote notes',
    tool_call_count: 0,
    ...overrides
  } as SessionInfo
}

describe('exportSession owner routing', () => {
  const getAll = vi.mocked(getAllSessionMessages)

  beforeEach(() => {
    getAll.mockResolvedValue({ session_id: '20260924_remote1', messages: [] })
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:export')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  })

  afterEach(() => {
    $sessions.set([])
    vi.clearAllMocks()
    vi.restoreAllMocks()
  })

  it('reads a registry-connection session through its owner route (Command Center passes the row)', async () => {
    const session = makeSession({ connection_id: 'sheldon', profile: 'tb-king' })

    await exportSession(session.id, { session, title: 'Remote notes' })

    expect(getAll).toHaveBeenCalledTimes(1)
    expect(getAll).toHaveBeenCalledWith(session.id, {
      connectionId: 'sheldon',
      profile: 'tb-king'
    })
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('resolves the owner from the cached session list when only a profile is passed (sidebar menu)', async () => {
    const session = makeSession({ connection_id: 'snowdrop', id: '20260924_remote2', profile: 'worker' })
    $sessions.set([session])

    await exportSession(session.id, { profile: 'worker', title: 'Remote notes' })

    expect(getAll).toHaveBeenCalledWith(session.id, {
      connectionId: 'snowdrop',
      profile: 'worker'
    })
  })

  it('keeps the plain profile scope for local sessions without a connection tag', async () => {
    const session = makeSession({ id: '20260924_local1', profile: 'default' })

    await exportSession(session.id, { session })
    expect(getAll).toHaveBeenLastCalledWith(session.id, 'default')

    await exportSession('20260924_unlisted', { profile: 'research' })
    expect(getAll).toHaveBeenLastCalledWith('20260924_unlisted', 'research')
  })
})
