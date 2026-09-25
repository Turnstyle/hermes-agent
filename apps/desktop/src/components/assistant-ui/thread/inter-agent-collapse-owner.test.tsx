// `display.collapse_inter_agent_replies` must follow the profile that OWNS the
// open chat, not the window's active profile (t_e15febb6).
//
// A Bot Chat opens without switching the window's profile
// (planPluginOpenSession keeps chrome on the launch profile), so the global
// config refresh — keyed on the active gateway scope — never re-runs for the
// bot. Live: tb-cndr's config said false, the window sat on tb-core-ea, and
// tb-cndr's Bot Chat still folded every reply to a bot report.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  _resetOwnerCollapseSettingsForTests,
  refreshOwnerCollapseSettings,
  setCollapseInterAgentRepliesFromConfig
} from '@/store/inter-agent-collapse'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $connection,
  $selectedStoredSessionId,
  $sessions,
  _resetSessionOwnerHintsForTests,
  forgetSessionOwnerHintsForSession,
  setSessionOwnerHint
} from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { Thread } from '.'

const createdAt = new Date('2026-05-01T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}

Element.prototype.animate = function animate() {
  return { cancel() {}, finished: Promise.resolve() } as unknown as Animation
}

const assistantMetadata = { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }

function user(id: string, text: string): ThreadMessage {
  return {
    id,
    role: 'user',
    content: [{ type: 'text', text }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  } as ThreadMessage
}

function assistant(id: string, text: string): ThreadMessage {
  return {
    id,
    role: 'assistant',
    content: [{ type: 'text', text }],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: assistantMetadata
  } as ThreadMessage
}

function Harness({ messages }: { messages: ThreadMessage[] }) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({ messages, isRunning: false, onNew: async () => {} })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

const DELIVERY = 'Message from 🤖 Snow (@snow-king): deploy finished, 3 checks green'
const REPORT = 'report for Turner: deploy is green'
const MESSAGES = [user('u1', DELIVERY), assistant('a1', REPORT)]

/** Config as each backend serves it, keyed `connectionId::profile`. Only
 *  `botx` on this machine, and `botz` on remote-a, turn the fold off. */
const BASE_CONFIG: Record<string, Record<string, unknown>> = {
  'local::botx': { display: { collapse_inter_agent_replies: false } },
  'local::boty': { display: {} },
  'remote-a::botz': { display: { collapse_inter_agent_replies: false } },
  'remote-b::botz': { display: {} }
}

let config: Record<string, Record<string, unknown>> = {}
let api: ReturnType<typeof vi.fn>
/** When true, config reads wait in `held` until released (in any order). */
let holding = false
let held: Array<() => void> = []

/** `connectionId::profile` of every /api/config read (other paths ignored). */
function configReads(): string[] {
  return api.mock.calls
    .map(([request]) => request as { connectionId?: string; path: string; profile?: string })
    .filter(request => request.path.startsWith('/api/config'))
    .map(request => `${request.connectionId ?? ''}::${request.profile ?? ''}`)
}

beforeEach(() => {
  config = { ...BASE_CONFIG }
  holding = false
  held = []
  api = vi.fn(async (request: { connectionId?: string; path: string; profile?: string }) => {
    if (!request.path.startsWith('/api/config')) {
      throw new Error(`unexpected ${request.path}`)
    }

    // Snapshot at SEND time, like a real backend answering this request.
    const value = config[`${request.connectionId ?? ''}::${request.profile ?? ''}`] ?? {}

    return holding ? new Promise(resolve => held.push(() => resolve(value))) : value
  })
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { api }
  // The window's own (active) profile keeps the default: fold on.
  $activeGatewayProfile.set('default')
  setCollapseInterAgentRepliesFromConfig(undefined)
})

afterEach(() => {
  cleanup()
  $selectedStoredSessionId.set(null)
  $sessions.set([])
  $connection.set(null)
  _resetSessionOwnerHintsForTests({ storage: true })
  _resetOwnerCollapseSettingsForTests()
  setCollapseInterAgentRepliesFromConfig(undefined)
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

/** What host.openSession records for a local Bot Chat open (sdk/index.ts). */
function openBotChat(storedId: string, profile: string) {
  setSessionOwnerHint(storedId, { connectionId: 'local', mode: 'local', profile })
  $selectedStoredSessionId.set(storedId)
}

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

const folded = () => screen.queryByText(/Replied to/) !== null

describe('inter-agent collapse follows the chat owner', () => {
  it('open Bot Chat of a profile whose config says false -> no fold, even while the window profile folds', async () => {
    openBotChat('sid-botx', 'botx')
    render(<Harness messages={MESSAGES} />)

    await screen.findByText(REPORT)
    await waitFor(() => expect(configReads()).toContain('local::botx'))
    await waitFor(() => expect(folded()).toBe(false))
    expect(screen.queryByText('show reply')).toBeNull()
  })

  it('keeps the fold in a Bot Chat whose profile leaves the key unset', async () => {
    openBotChat('sid-boty', 'boty')
    render(<Harness messages={MESSAGES} />)

    await waitFor(() => expect(configReads()).toContain('local::boty'))
    expect(await screen.findByText(/Replied to/)).toBeTruthy()
  })

  it('keeps the fold for the window profile when the window profile says off but the chat owner does not', async () => {
    // Inverse leak: the ambient value must not override a known owner either.
    setCollapseInterAgentRepliesFromConfig(false)
    openBotChat('sid-boty', 'boty')
    render(<Harness messages={MESSAGES} />)

    await waitFor(() => expect(configReads()).toContain('local::boty'))
    expect(await screen.findByText(/Replied to/)).toBeTruthy()
  })

  it('uses the window profile value when the chat has no known owner', async () => {
    setCollapseInterAgentRepliesFromConfig(false)
    $selectedStoredSessionId.set('sid-unknown')
    render(<Harness messages={MESSAGES} />)

    await screen.findByText(REPORT)
    expect(folded()).toBe(false)
    expect(configReads()).toEqual([])
  })

  it('picks up an owner config change on the next config refresh', async () => {
    openBotChat('sid-botx', 'botx')
    render(<Harness messages={MESSAGES} />)
    await waitFor(() => expect(folded()).toBe(false))

    config['local::botx'] = { display: { collapse_inter_agent_replies: true } }
    refreshOwnerCollapseSettings()
    expect(await screen.findByText(/Replied to/)).toBeTruthy()
  })

  it('reads the owner when its owner record arrives after the chat mounted', async () => {
    $selectedStoredSessionId.set('sid-late')
    render(<Harness messages={MESSAGES} />)
    await screen.findByText(REPORT)
    expect(folded()).toBe(true)
    expect(configReads()).toEqual([])

    act(() => setSessionOwnerHint('sid-late', { connectionId: 'local', mode: 'local', profile: 'botx' }))

    await waitFor(() => expect(configReads()).toEqual(['local::botx']))
    await waitFor(() => expect(folded()).toBe(false))
  })

  it('ignores the previous owner\u2019s late answer after the owner changes', async () => {
    holding = true
    openBotChat('sid-move', 'botx')
    render(<Harness messages={MESSAGES} />)
    await waitFor(() => expect(configReads()).toEqual(['local::botx']))

    act(() => {
      forgetSessionOwnerHintsForSession('sid-move')
      setSessionOwnerHint('sid-move', { connectionId: 'local', mode: 'local', profile: 'boty' })
    })
    await waitFor(() => expect(configReads()).toEqual(['local::botx', 'local::boty']))

    // boty (fold on) answers first, then botx's stale "off" lands.
    held[1]()
    await flush()
    held[0]()
    await flush()

    expect(folded()).toBe(true)
  })

  it('pins a bare-profile owner to the serving connection and re-reads after a connection switch', async () => {
    // A session row with a profile but no connection tag (legacy/primary path).
    $sessions.set([{ id: 'sid-row', profile: 'botz' } as SessionInfo])
    $connection.set({ connectionId: 'remote-a', mode: 'remote' } as never)
    $selectedStoredSessionId.set('sid-row')
    render(<Harness messages={MESSAGES} />)

    await waitFor(() => expect(configReads()).toEqual(['remote-a::botz']))
    await waitFor(() => expect(folded()).toBe(false))

    // Another machine's same-named profile must not reuse remote-a's value.
    act(() => $connection.set({ connectionId: 'remote-b', mode: 'remote' } as never))

    await waitFor(() => expect(configReads()).toEqual(['remote-a::botz', 'remote-b::botz']))
    expect(await screen.findByText(/Replied to/)).toBeTruthy()
  })

  it('lets a refresh sent during an older read win over that older read', async () => {
    holding = true
    openBotChat('sid-botx', 'botx')
    render(<Harness messages={MESSAGES} />)
    await waitFor(() => expect(configReads()).toEqual(['local::botx']))

    // The user turns the fold back on while the first read (off) is in flight.
    config['local::botx'] = { display: { collapse_inter_agent_replies: true } }
    refreshOwnerCollapseSettings()
    expect(configReads()).toEqual(['local::botx', 'local::botx'])

    held[1]()
    await flush()
    held[0]()
    await flush()

    expect(folded()).toBe(true)
  })
})
