/**
 * `display.collapse_inter_agent_replies` — fold a settled reply to an inbound
 * "Message from <bot>" row into a compact "Replied to <bot>" notice.
 *
 * On by default (Grok-bots parity, #85884). A Conductor's reply to a bot
 * report is often the report to the human, and folding it made that report
 * look like it vanished the moment it settled. Profiles where that is the
 * norm set this key to false in their config.yaml.
 *
 * Display-only: reading or toggling it never mutates model context, so it is
 * prompt-cache safe. A quoted "false" in config.yaml still means off
 * (siblings: display-timestamps.ts, reasoning-disclosure.ts).
 */

import { atom } from 'nanostores'

export const $collapseInterAgentReplies = atom<boolean>(true)

export function setCollapseInterAgentRepliesFromConfig(value: unknown): void {
  $collapseInterAgentReplies.set(!(value === false || value === 'false' || value === 0))
}
