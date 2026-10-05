"""Slack-platform concise ``/help`` (``!help``) body for mobile / thread UX.

Kept beside the gateway slash mixins so ``_handle_help_command`` can branch without
growing a second catalog in the Slack adapter. Only lists registry commands that are
actually gateway-supported; reaction lines read the *receiving* profile's adapter
evidence, never another profile's config.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command

# Short control subset for Slack !help (Conductor decision). Include only when the
# command resolves in the registry and survives authorization filtering.
_SLACK_CONTROL_ORDER = ("help", "status", "busy", "stop", "queue", "steer")

# Traced grammar / semantics (CommandDef + gateway handlers). Bang form is what mobile
# threads use; slash is equivalent after rewrite.
_SLACK_CONTROL_LINES = {
    "help": "`!help` — this short guide (`!commands` for the full catalog)",
    "status": "`!status` — session, model, token, and context info",
    "busy": (
        "`!busy [queue|steer|interrupt|status]` — how messages behave while Hermes is "
        "working (example: `!busy queue`)"
    ),
    "stop": "`!stop` — interrupt the running agent in this chat/thread session",
    "queue": (
        "`!queue <prompt>` — queue a prompt for the next turn "
        "(alias `!q`; example: `!queue check logs`)"
    ),
    "steer": (
        "`!steer <prompt>` — inject after the next tool call without interrupting "
        "(example: `!steer focus on the failing test`)"
    ),
}

# Proposed reaction key from CoS state.db session 20261001_214046_f6e50ce9
# (USER8130 / ASSISTANT8189). Explicitly NOT LIVE — never label as configured/active.
_PROPOSED_REACTION_KEY = (
    ("🔁", "follow up"),
    ("🏃", "run this down"),
    ("🟢", "green light referenced path"),
    ("⏸️", "pause effort"),
    ("🔴", "stop work"),
    ("💬", "explain more"),
    ("🔬", "research deeper"),
    ("🎫", "create Hermes kanban card"),
    ("📋", "add to Slack Fleet List with a 30-character issue title"),
)


def format_reaction_status(
    triggers: Optional[set], *, known: bool = True,
) -> str:
    """Human status for this profile's ``reaction_triggers`` evidence."""
    if not known:
        return "unknown (no receiving Slack adapter evidence for this session)"
    if triggers is None:
        return "not enabled"
    if not triggers:
        return (
            "enabled for reactions on this bot's own messages "
            "(all emoji; `platforms.slack.extra.reaction_triggers: true`)"
        )
    names = ", ".join(f":{name}:" for name in sorted(triggers))
    return f"enabled for emoji allowlist on this profile: {names}"


def _control_names(allowed_commands: Optional[Iterable[str]]) -> list[str]:
    allowed = None if allowed_commands is None else set(allowed_commands)
    names: list[str] = []
    for name in _SLACK_CONTROL_ORDER:
        if resolve_command(name) is None:
            continue
        if allowed is not None and name not in allowed:
            continue
        # Prefer registry presence over inventing surface area.
        if not any(cmd.name == name for cmd in COMMAND_REGISTRY):
            continue
        names.append(name)
    return names


def build_slack_help_text(
    *,
    allowed_commands: Optional[Iterable[str]] = None,
    reaction_triggers: Optional[set] = None,
    reaction_status_known: bool = True,
    configured_slash_aliases: Optional[Mapping[str, str]] = None,
    receiving_profile: Optional[str] = None,
) -> str:
    """Build the Slack-only concise help body (markdown)."""
    profile_note = f" `{receiving_profile}`" if receiving_profile else ""
    lines = [
        "*Hermes Slack controls* (this app / profile only)",
        "",
        "*Controls*",
    ]
    for name in _control_names(allowed_commands):
        lines.append(f"• {_SLACK_CONTROL_LINES[name]}")
    lines.extend([
        "",
        "*Targeting*",
        "• In a channel, mention the Slack app you want, then the command "
        "(`@AppName !status`), or DM / thread that same app.",
        "• Replies stay in the receiving app's session for that chat/thread.",
        "• The receiving Slack app session is *not* automatically canonical Bot Chat "
        "and does *not* cross-dispatch to other profiles.",
        "• Canonical agent-to-agent work uses `message_agent` (same node) or peer run "
        "(other node) with real profile routing — arbitrary Slack @mentions do not "
        "dispatch kanban cards.",
        "",
        f"*Reactions* (receiving profile{profile_note})",
        f"• Configured now: {format_reaction_status(reaction_triggers, known=reaction_status_known)}",
        "• When enabled, routing is event-driven (no polling).",
        "• Proposed, not active (reminder key only — not wired on this profile):",
    ])
    for emoji, meaning in _PROPOSED_REACTION_KEY:
        lines.append(f"  {emoji} {meaning}")

    aliases = dict(configured_slash_aliases or {})
    lines.extend(["", "*Slash aliases* (this profile only)"])
    if aliases:
        for alias, target in sorted(aliases.items()):
            lines.append(f"• `/{alias}` → `/{target}`")
    else:
        lines.append(
            "• None configured on this profile "
            "(`platforms.slack.extra.slash_aliases`). "
            "`/cos-*` aliases are not listed as live until present here "
            "(manifest setup is separate)."
        )

    lines.extend([
        "",
        "Full catalog: `!commands` (or `/commands`).",
    ])
    return "\n".join(lines)
