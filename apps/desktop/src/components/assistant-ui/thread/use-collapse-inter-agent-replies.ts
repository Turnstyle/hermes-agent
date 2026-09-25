import { useStore } from '@nanostores/react'
import { computed } from 'nanostores'
import { useEffect, useMemo } from 'react'

import { profileScopeKey } from '@/api/client'
import { useSessionView } from '@/app/chat/session-view'
import {
  $collapseInterAgentReplies,
  $ownerCollapseSettings,
  type CollapseOwnerScope,
  ensureOwnerCollapseSetting
} from '@/store/inter-agent-collapse'
import {
  $connection,
  $cronSessions,
  $messagingSessions,
  $sessionOwnerHintsVersion,
  $sessions,
  $unlistedSessionOwnerRows
} from '@/store/session'
import { $sessionTiles, knownOwnerForSession } from '@/store/session-states'

/**
 * The exact (connection, profile) whose config governs this chat, or
 * undefined when unknown. A bare-profile owner is pinned to the connection
 * serving the window right now — the backend a bare-profile read would dial
 * anyway — so its cached value can never be reused for another machine's
 * same-named profile.
 */
function chatOwnerScope(storedId: null | string): CollapseOwnerScope | undefined {
  const owner = storedId ? knownOwnerForSession(storedId) : undefined

  if (!owner) {
    return undefined
  }

  if (typeof owner === 'string') {
    const profile = owner.trim()
    const connection = $connection.get()
    const connectionId = (connection?.connectionId || (connection?.mode === 'local' ? 'local' : '')).trim()

    return profile && connectionId ? { connectionId, profile } : undefined
  }

  const profile = (owner.targetProfile || owner.profile || '').trim()
  const connectionId = (owner.connectionId || '').trim()

  return profile && connectionId ? { connectionId, profile } : undefined
}

/**
 * Whether a settled reply to an inter-agent delivery folds in THIS chat.
 *
 * Follows the profile that owns the chat on screen (tile route, owner hint,
 * session row — the same ladder session RPCs use), not the window's active
 * profile: a Bot Chat is opened without switching the window's profile, so
 * the window value belongs to another profile (t_e15febb6). Until the owner's
 * own value arrives — or when the owner is unknown — the window value stands.
 *
 * The owner is re-resolved whenever an input of that ladder changes (an owner
 * hint recorded after mount, a tile route, a session-list refresh, a
 * connection switch). The derived value is a string, so a row re-renders only
 * when its resolved owner actually changes — never per streamed token.
 */
export function useCollapseInterAgentReplies(): boolean {
  const storedId = useStore(useSessionView().$storedId)
  const ambient = useStore($collapseInterAgentReplies)
  const settings = useStore($ownerCollapseSettings)

  const $ownerJson = useMemo(
    () =>
      computed(
        [
          $sessionTiles,
          $sessionOwnerHintsVersion,
          $sessions,
          $cronSessions,
          $messagingSessions,
          $unlistedSessionOwnerRows,
          $connection
        ],
        () => {
          const scope = chatOwnerScope(storedId)

          return scope ? JSON.stringify(scope) : ''
        }
      ),
    [storedId]
  )

  const ownerJson = useStore($ownerJson)
  const scope = useMemo(() => (ownerJson ? (JSON.parse(ownerJson) as CollapseOwnerScope) : undefined), [ownerJson])

  // Idempotent and freshness-gated: every row of one chat shares one read.
  useEffect(() => {
    if (scope) {
      ensureOwnerCollapseSetting(scope)
    }
  }, [scope])

  // Only the CURRENT owner's value counts: after an owner change the previous
  // owner's cached value is keyed elsewhere and cannot leak in.
  const owned = scope ? settings[profileScopeKey(scope)] : undefined

  return owned ?? ambient
}
