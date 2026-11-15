import { cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { ComposerControls } from '@/app/chat/composer/controls'
import { useComposerSubmit } from '@/app/chat/composer/hooks/use-composer-submit'

vi.mock('@/app/chat/composer/model-pill', () => ({ ModelPill: () => null }))
afterEach(cleanup)
it('the empty busy composer Stop button still cancels under steer policy', () => {
  const onCancel = vi.fn()
  const onSteer = vi.fn()
  const onSubmit = vi.fn(() => true)

  const hook = renderHook(() =>
    useComposerSubmit({
      activeQueueSessionKey: 'owner',
      activeQueueSessionKeyRef: { current: 'owner' },
      attachments: [],
      busy: true,
      busyInputMode: 'steer',
      clearDraft: vi.fn(),
      disabled: false,
      draftScopeRef: { current: 'owner' },
      draftRef: { current: '' },
      drainNextQueued: vi.fn(async () => false),
      editorRef: { current: null },
      exitQueuedEdit: vi.fn(() => false),
      focusInput: vi.fn(),
      inputDisabled: false,
      loadIntoComposer: vi.fn(),
      onCancel,
      onSteer,
      onSteerHidden: undefined,
      onSubmit,
      queueCurrentDraft: vi.fn(() => false),
      queueEdit: null,
      queuedPrompts: [],
      sessionId: 'owner',
      setComposerText: vi.fn(),
      stashAt: vi.fn()
    })
  )

  render(
    <form
      onSubmit={event => {
        event.preventDefault()
        hook.result.current.submitDraft()
      }}
    >
      <ComposerControls
        autoSpeak={false}
        busy
        busyAction="stop"
        canSubmit
        conversation={{
          active: false,
          level: 0,
          muted: false,
          status: 'idle',
          onEnd: vi.fn(),
          onStart: vi.fn(),
          onStopTurn: vi.fn(),
          onToggleMute: vi.fn()
        }}
        disabled={false}
        hasComposerPayload={false}
        onDictate={vi.fn()}
        onQueue={vi.fn()}
        onToggleAutoSpeak={vi.fn()}
        state={{
          model: { canSwitch: false, model: '', provider: '' },
          tools: { enabled: false, label: '' },
          voice: { active: false, enabled: false }
        }}
        voiceStatus="idle"
      />
    </form>
  )
  fireEvent.click(screen.getByLabelText('Stop'))
  expect(onCancel).toHaveBeenCalledTimes(1)
  expect(onSteer).not.toHaveBeenCalled()
  expect(onSubmit).not.toHaveBeenCalled()
})
