# Slack !help SOURCE artifact (t_64f38cfe)

Real executed output from this worktree (not installed tree).

## Import path proof
- gateway.slash_commands_slack_help: `/Users/sheldon/.hermes/kanban/boards/fleet/workspaces/t_64f38cfe/source/gateway/slash_commands_slack_help.py`
- plugins.platforms.slack.adapter: `/Users/sheldon/.hermes/kanban/boards/fleet/workspaces/t_64f38cfe/source/plugins/platforms/slack/adapter.py`

## Bang rewrite → MessageEvent.get_command()

| input | rewritten | get_command |
|---|---|---|
| `!help` | `/help` | `help` |
| `!Help` | `/Help` | `help` |
| `! Help` | `/Help` | `help` |
| `!   HELP` | `/HELP` | `help` |
| `  !help` | `/help` | `help` |
| `!busy queue` | `/busy queue` | `busy` |
| `!steer keep  spaces` | `/steer keep  spaces` | `steer` |
| `!nice work` | `!nice work` | `None` |

## Executed Slack !help — reactions not enabled (receiving profile shld-core-cos)

```
*Hermes Slack controls* (this app / profile only)

*Controls*
• `!help` — this short guide (`!commands` for the full catalog)
• `!status` — session, model, token, and context info
• `!busy [queue|steer|interrupt|status]` — how messages behave while Hermes is working (example: `!busy queue`)
• `!stop` — interrupt the running agent in this chat/thread session
• `!queue <prompt>` — queue a prompt for the next turn (alias `!q`; example: `!queue check logs`)
• `!steer <prompt>` — inject after the next tool call without interrupting (example: `!steer focus on the failing test`)

*Targeting*
• In a channel, mention the Slack app you want, then the command (`@AppName !status`), or DM / thread that same app.
• Replies stay in the receiving app's session for that chat/thread.
• The receiving Slack app session is *not* automatically canonical Bot Chat and does *not* cross-dispatch to other profiles.
• Canonical agent-to-agent work uses `message_agent` (same node) or peer run (other node) with real profile routing — arbitrary Slack @mentions do not dispatch kanban cards.

*Reactions* (receiving profile `shld-core-cos`)
• Configured now: not enabled
• When enabled, routing is event-driven (no polling).
• Proposed, not active (reminder key only — not wired on this profile):
  🔁 follow up
  🏃 run this down
  🟢 green light referenced path
  ⏸️ pause effort
  🔴 stop work
  💬 explain more
  🔬 research deeper
  🎫 create Hermes kanban card
  📋 add to Slack Fleet List with a 30-character issue title

*Slash aliases* (this profile only)
• None configured on this profile (`platforms.slack.extra.slash_aliases`). `/cos-*` aliases are not listed as live until present here (manifest setup is separate).

Full catalog: `!commands` (or `/commands`).
```

## Executed Slack !help — configured fixture (reaction allowlist + slash alias)

```
*Hermes Slack controls* (this app / profile only)

*Controls*
• `!help` — this short guide (`!commands` for the full catalog)
• `!status` — session, model, token, and context info
• `!busy [queue|steer|interrupt|status]` — how messages behave while Hermes is working (example: `!busy queue`)
• `!stop` — interrupt the running agent in this chat/thread session
• `!queue <prompt>` — queue a prompt for the next turn (alias `!q`; example: `!queue check logs`)
• `!steer <prompt>` — inject after the next tool call without interrupting (example: `!steer focus on the failing test`)

*Targeting*
• In a channel, mention the Slack app you want, then the command (`@AppName !status`), or DM / thread that same app.
• Replies stay in the receiving app's session for that chat/thread.
• The receiving Slack app session is *not* automatically canonical Bot Chat and does *not* cross-dispatch to other profiles.
• Canonical agent-to-agent work uses `message_agent` (same node) or peer run (other node) with real profile routing — arbitrary Slack @mentions do not dispatch kanban cards.

*Reactions* (receiving profile `fixture-profile`)
• Configured now: enabled for emoji allowlist on this profile: :task:, :thumbsup:
• When enabled, routing is event-driven (no polling).
• Proposed, not active (reminder key only — not wired on this profile):
  🔁 follow up
  🏃 run this down
  🟢 green light referenced path
  ⏸️ pause effort
  🔴 stop work
  💬 explain more
  🔬 research deeper
  🎫 create Hermes kanban card
  📋 add to Slack Fleet List with a 30-character issue title

*Slash aliases* (this profile only)
• `/cos-busy` → `/busy`

Full catalog: `!commands` (or `/commands`).
```

## Non-Slack help
Telegram/Discord `/help` still uses shared `slash_exec` catalog (see `test_non_slack_help_unchanged_shared_catalog`).

## !commands
`/commands` on Slack still uses shared paginated catalog (see `test_commands_still_full_catalog_on_slack`).
