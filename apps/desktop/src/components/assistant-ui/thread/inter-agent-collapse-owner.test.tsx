// `display.collapse_inter_agent_replies` must follow the profile that OWNS the
// open chat, not the window's active profile (t_e15febb6).
//
// A Bot Chat opens without switching the window's profile
// (planPluginOpenSession keeps chrome on the launch profile), so the global
// config refresh — keyed on the active gateway scope — never re-runs for the
// bot. Live: tb-cndr's config said false, the window sat on tb-core-ea, and
// tb-cndr's Bot Chat still folded every reply to a bot report.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  _resetOwnerCollapseSettingsForTests,
  refreshOwnerCollapseSettings,
  setCollapseInterAgentRepliesFromConfig
} from '@/store/inter-agent-collapse'
import { $activeGatewayProfile } from '@/store/profile'
import { $selectedStoredSessionId, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'

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

/** Per-profile config as the backend serves it. Only `botx` turns the fold off. */
const CONFIG_BY_PROFILE: Record<string, Record<string, unknown>> = {
  botx: { display: { collapse_inter_agent_replies: false } },
  boty: { display: {} },
  default: { display: {} }
}

let api: ReturnType<typeof vi.fn>

/** Profiles whose /api/config this test saw read (other paths ignored). */
function configReads(): string[] {
  return api.mock.calls
    .map(([request]) => request as { path: string; profile?: string })
    .filter(request => request.path.startsWith('/api/config'))
    .map(request => request.profile ?? 'default')
}

beforeEach(() => {
  api = vi.fn(async (request: { path: string; profile?: string }) => {
    if (!request.path.startsWith('/api/config')) {
      throw new Error(`unexpected ${request.path}`)
    }

    return CONFIG_BY_PROFILE[request.profile ?? 'default'] ?? {}
  })
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { api }
  // The window's own (active) profile keeps the default: fold on.
  $activeGatewayProfile.set('default')
  setCollapseInterAgentRepliesFromConfig(undefined)
})

afterEach(() => {
  cleanup()
  $selectedStoredSessionId.set(null)
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

describe('inter-agent collapse follows the chat owner', () => {
  it('open Bot Chat of a profile whose config says false -> no fold, even while the window profile folds', async () => {
    openBotChat('sid-botx', 'botx')
    render(<Harness messages={MESSAGES} />)

    await screen.findByText(REPORT)
    await waitFor(() => expect(configReads()).toContain('botx'))
    await waitFor(() => expect(screen.queryByText(/Replied to/)).toBeNull())
    expect(screen.queryByText('show reply')).toBeNull()
  })

  it('keeps the fold in a Bot Chat whose profile leaves the key unset', async () => {
    openBotChat('sid-boty', 'boty')
    render(<Harness messages={MESSAGES} />)

    await waitFor(() => expect(configReads()).toContain('boty'))
    expect(await screen.findByText(/Replied to/)).toBeTruthy()
  })

  it('keeps the fold for the window profile when the window profile says off but the chat owner does not', async () => {
    // Inverse leak: the ambient value must not override a known owner either.
    setCollapseInterAgentRepliesFromConfig(false)
    openBotChat('sid-boty', 'boty')
    render(<Harness messages={MESSAGES} />)

    await waitFor(() => expect(configReads()).toContain('boty'))
    expect(await screen.findByText(/Replied to/)).toBeTruthy()
  })

  it('uses the window profile value when the chat has no known owner', async () => {
    setCollapseInterAgentRepliesFromConfig(false)
    $selectedStoredSessionId.set('sid-unknown')
    render(<Harness messages={MESSAGES} />)

    await screen.findByText(REPORT)
    expect(screen.queryByText(/Replied to/)).toBeNull()
    expect(configReads()).toEqual([])
  })

  it('picks up an owner config change on the next config refresh', async () => {
    openBotChat('sid-botx', 'botx')
    render(<Harness messages={MESSAGES} />)
    await waitFor(() => expect(screen.queryByText(/Replied to/)).toBeNull())

    CONFIG_BY_PROFILE.botx = { display: { collapse_inter_agent_replies: true } }

    try {
      refreshOwnerCollapseSettings()
      expect(await screen.findByText(/Replied to/)).toBeTruthy()
    } finally {
      CONFIG_BY_PROFILE.botx = { display: { collapse_inter_agent_replies: false } }
    }
  })
})
