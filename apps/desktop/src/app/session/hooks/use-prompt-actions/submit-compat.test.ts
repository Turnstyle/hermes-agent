import { JsonRpcGatewayError } from '@hermes/shared'
import { describe, expect, it, vi } from 'vitest'

import { isUnsupportedClientMessageIdError, requestPromptSubmit } from './utils'

// The exact refusal a strict gateway that predates client_message_id returns (rpc_dispatch 4000).
const OLD_GATEWAY_REFUSAL = new JsonRpcGatewayError(
  'invalid params for prompt.submit: client_message_id: Extra inputs are not permitted — the client and the Hermes backend are out of sync (different versions); run `hermes update` and restart both',
  { code: 4000 }
)

describe('prompt.submit against a gateway without occurrence receipts (t_edb5b1dd B4)', () => {
  it('resends once without client_message_id on the unsupported-parameter refusal', async () => {
    const request = vi.fn().mockRejectedValueOnce(OLD_GATEWAY_REFUSAL).mockResolvedValueOnce({ status: 'streaming' })

    await expect(
      requestPromptSubmit(request, { session_id: 's', text: 'hi', client_message_id: 'user-1' })
    ).resolves.toEqual({ status: 'streaming' })

    expect(request.mock.calls.map(call => call[0])).toEqual([
      { session_id: 's', text: 'hi', client_message_id: 'user-1' },
      { session_id: 's', text: 'hi' }
    ])
  })

  it('never retries any other failure', async () => {
    const other = new JsonRpcGatewayError('invalid params for prompt.submit: text: field required', { code: 4000 })
    const request = vi.fn().mockRejectedValue(other)

    await expect(requestPromptSubmit(request, { session_id: 's', client_message_id: 'user-1' })).rejects.toBe(other)
    expect(request).toHaveBeenCalledOnce()
    expect(isUnsupportedClientMessageIdError(new Error('session busy'))).toBe(false)
  })
})
