import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import { profileScopeKey } from '@/api/client'
import { useSessionView } from '@/app/chat/session-view'
import {
  $collapseInterAgentReplies,
  $ownerCollapseSettings,
  ensureOwnerCollapseSetting
} from '@/store/inter-agent-collapse'
import { knownOwnerForSession } from '@/store/session-states'

type OwnerScope = { connectionId?: string; profile: string } | string

/** The (connection, profile) whose config governs this chat, or undefined. */
function chatOwnerScope(storedId: null | string): OwnerScope | undefined {
  const owner = storedId ? knownOwnerForSession(storedId) : undefined

  if (!owner) {
    return undefined
  }

  if (typeof owner === 'string') {
    return owner.trim() || undefined
  }

  const profile = (owner.targetProfile || owner.profile || '').trim()

  return profile ? { connectionId: owner.connectionId || undefined, profile } : undefined
}

/**
 * Whether a settled reply to an inter-agent delivery folds in THIS chat.
 *
 * Follows the profile that owns the chat on screen (tile route, owner hint,
 * session row — the same ladder session RPCs use), not the window's active
 * profile: a Bot Chat is opened without switching the window's profile, so
 * the window value belongs to another profile (t_e15febb6). Until the owner's
 * own value arrives — or when the owner is unknown — the window value stands.
 */
export function useCollapseInterAgentReplies(): boolean {
  const storedId = useStore(useSessionView().$storedId)
  const ambient = useStore($collapseInterAgentReplies)
  const settings = useStore($ownerCollapseSettings)
  const scope = useMemo(() => chatOwnerScope(storedId), [storedId])
  const scopeJson = scope ? JSON.stringify(scope) : ''

  const key = scope ? profileScopeKey(scope) : ''

  // Idempotent and freshness-gated: every row of one chat shares one read.
  useEffect(() => {
    if (scopeJson) {
      ensureOwnerCollapseSetting(JSON.parse(scopeJson) as OwnerScope)
    }
  }, [scopeJson])

  const owned = key ? settings[key] : undefined

  return owned ?? ambient
}
