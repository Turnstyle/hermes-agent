/**
 * `display.collapse_inter_agent_replies` — fold a settled reply to an inbound
 * "Message from <bot>" row into a compact "Replied to <bot>" notice.
 *
 * On by default (Grok-bots parity, #85884). A Conductor's reply to a bot
 * report is often the report to the human, and folding it made that report
 * look like it vanished the moment it settled. Profiles where that is the
 * norm set this key to false in their config.yaml.
 *
 * The key is PER PROFILE, and it must follow the profile that OWNS the chat on
 * screen. A Bot Chat opens without switching the window's profile
 * (planPluginOpenSession keeps chrome on the launch profile), so the window's
 * config refresh never runs for the bot. `$collapseInterAgentReplies` is the
 * window (ambient) profile's value; `$ownerCollapseSettings` caches each chat
 * owner's own value, read from that owner's backend (t_e15febb6).
 *
 * Display-only: reading or toggling it never mutates model context, so it is
 * prompt-cache safe. A quoted "false" in config.yaml still means off
 * (siblings: display-timestamps.ts, reasoning-disclosure.ts).
 */

import { atom } from 'nanostores'

import { profileScopeKey } from '@/api/client'
import { getHermesConfigRecord } from '@/api/config'

/** The window (active profile) value. Fallback for a chat with no known owner. */
export const $collapseInterAgentReplies = atom<boolean>(true)

export function collapseFromConfigValue(value: unknown): boolean {
  return !(value === false || value === 'false' || value === 0)
}

export function setCollapseInterAgentRepliesFromConfig(value: unknown): void {
  $collapseInterAgentReplies.set(collapseFromConfigValue(value))
}

/** An exact chat owner. Always connection-pinned, so the cache key
 *  (`connectionId::profile`) can never be shared by two machines' same-named
 *  profiles and the read always dials that exact backend. */
export interface CollapseOwnerScope {
  connectionId: string
  profile: string
}

/** Chat-owner values keyed by profileScopeKey. A missing key = not known yet. */
export const $ownerCollapseSettings = atom<Readonly<Record<string, boolean>>>({})

/** A read this recent is reused instead of re-fetched (many rows mount at once). */
const FRESH_MS = 5_000
/** Bounded retries for a cold owner backend; never a loop. */
const RETRY_DELAYS_MS = [2_000, 6_000] as const

interface OwnerEntry {
  fetchedAt: number
  /** Latest read issued for this owner. Only that read may publish, so a
   *  forced refresh supersedes an older read still in flight. */
  generation: number
  inflight: boolean
  retryTimer: null | ReturnType<typeof setTimeout>
  scope: CollapseOwnerScope
}

const owners = new Map<string, OwnerEntry>()
// Bumped on reset so a read that lands afterwards cannot publish.
let epoch = 0

function publish(key: string, value: boolean): void {
  const current = $ownerCollapseSettings.get()

  if (current[key] !== value) {
    $ownerCollapseSettings.set({ ...current, [key]: value })
  }
}

function startRead(key: string, entry: OwnerEntry, attempt: number): void {
  if (entry.retryTimer) {
    clearTimeout(entry.retryTimer)
    entry.retryTimer = null
  }

  const startedEpoch = epoch
  const generation = ++entry.generation
  const current = () => startedEpoch === epoch && generation === entry.generation

  entry.inflight = true

  getHermesConfigRecord({ connectionId: entry.scope.connectionId, profile: entry.scope.profile })
    .then(config => {
      if (!current()) {
        return
      }

      entry.inflight = false
      entry.fetchedAt = Date.now()
      const display = (config as { display?: { collapse_inter_agent_replies?: unknown } }).display
      publish(key, collapseFromConfigValue(display?.collapse_inter_agent_replies))
    })
    .catch(() => {
      if (!current()) {
        return
      }

      entry.inflight = false

      if (attempt >= RETRY_DELAYS_MS.length) {
        // Out of retries: the chat keeps the window value until the next
        // refresh or reopen asks again.
        return
      }

      entry.retryTimer = setTimeout(() => {
        entry.retryTimer = null

        if (current()) {
          startRead(key, entry, attempt + 1)
        }
      }, RETRY_DELAYS_MS[attempt])
    })
}

/**
 * Make sure the owner's own value is (being) read. Idempotent: rows of one
 * chat share a single request. `force` re-reads even a fresh value and
 * supersedes a read already in flight (the config may have changed after that
 * read was sent); the cached value stays on screen until the new one lands.
 * Returns the cache key for `$ownerCollapseSettings`.
 */
export function ensureOwnerCollapseSetting(
  scope: CollapseOwnerScope,
  { force = false }: { force?: boolean } = {}
): string {
  const key = profileScopeKey(scope)
  let entry = owners.get(key)

  if (!entry) {
    entry = { fetchedAt: 0, generation: 0, inflight: false, retryTimer: null, scope }
    owners.set(key, entry)
  }

  if (!force && (entry.inflight || entry.retryTimer || Date.now() - entry.fetchedAt < FRESH_MS)) {
    return key
  }

  startRead(key, entry, 0)

  return key
}

/** Re-read every owner seen this run (a handful of profiles at most). Called
 *  wherever the window config is refreshed. */
export function refreshOwnerCollapseSettings(): void {
  for (const [key, entry] of owners) {
    startRead(key, entry, 0)
  }
}

export function _resetOwnerCollapseSettingsForTests(): void {
  epoch += 1

  for (const entry of owners.values()) {
    if (entry.retryTimer) {
      clearTimeout(entry.retryTimer)
    }
  }

  owners.clear()
  $ownerCollapseSettings.set({})
}
