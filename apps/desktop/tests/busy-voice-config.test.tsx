import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { useBusyInputMode } from '@/app/session/hooks/use-busy-input-mode'
import { $connection, $gatewayState } from '@/store/session'
vi.mock('@/store/session', async () => {
  const { atom } = await import('nanostores')

  return {
    $gatewayState: atom('closed'),
    $connection: atom(null),
    knownSessionOwner: () => undefined,
    ownerLookupSessionRows: () => []
  }
})
vi.mock('@/store/profile', async () => {
  const { atom } = await import('nanostores')

  return { $activeGatewayProfile: atom('default') }
})
vi.mock('@/store/composer-queue', () => ({ serverOwnsComposerQueue: () => true }))
vi.mock('@/store/session-states', () => ({ sessionTileOwnerRoute: () => undefined }))
vi.mock('@/store/session-request-router', () => ({
  isSessionOwnerRoute: () => false,
  requestForSessionProfile: (
    _owner: unknown,
    request: (method: string, params: unknown) => unknown,
    method: string,
    params: unknown
  ) => request(method, params)
}))
afterEach(() => {
  cleanup()
  $gatewayState.set('closed')
  $connection.set(null)
})
it.each([undefined, null, '', 'nonsense', 'queue', 'steer', 'interrupt'])(
  'reads raw display busy mode %s for voice',
  async mode => {
    $gatewayState.set('open')

    const request = vi
      .fn<Parameters<typeof useBusyInputMode>[0]['requestGateway']>()
      .mockResolvedValue({ config: { display: { busy_input_mode: mode } } })

    const hook = renderHook(() =>
      useBusyInputMode({
        sessionId: 'voice-owner',
        storedSessionId: null,
        requestGateway: request as Parameters<typeof useBusyInputMode>[0]['requestGateway'],
        voice: true
      })
    )

    await waitFor(() => expect(hook.result.current).toBe(mode === 'steer' || mode === 'interrupt' ? mode : 'queue'), {
      timeout: 1000
    })
    expect(request).toHaveBeenCalledWith('config.get', { key: 'full', session_id: 'voice-owner' })
  }
)
